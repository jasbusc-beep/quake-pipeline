"""Structured logging: one JSON object per pipeline event.

Only counts, ids, timings and object names are logged. Never secrets,
environment dumps or API payloads.
"""

import json
import logging

logger = logging.getLogger("quake_pipeline")


def log_event(event: str, **fields) -> None:
    logger.info(json.dumps({"event": event, **fields}, sort_keys=True))
