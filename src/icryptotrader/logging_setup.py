"""Structured logging setup with JSON output for production and readable output for dev."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging / telemetry ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        import orjson

        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exc"] = self.formatException(record.exc_info)
        return orjson.dumps(entry).decode()


def setup_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure root logger. Call once at startup."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear existing handlers
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
