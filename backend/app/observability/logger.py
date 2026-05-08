"""Structlog configuration.

Two rendering modes:

- **Production (``DEBUG=false``)**: JSON lines on stdout. One JSON object
  per log event, suitable for shipping to a log aggregator.
- **Development (``DEBUG=true``)**: human-friendly coloured console
  output via :class:`structlog.dev.ConsoleRenderer`.

Call :func:`configure_logging` once at process startup. Module loggers
should then be obtained with :func:`get_logger`.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog
from structlog.types import Processor

from app.config import get_settings

_CONFIGURED = False


def configure_logging() -> None:
    """Configure structlog and the stdlib logger. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.debug:
        renderer: Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)

    _CONFIGURED = True


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger, optionally bound with initial context."""
    if not _CONFIGURED:
        configure_logging()
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    if initial_values:
        logger = logger.bind(**initial_values)
    return cast(structlog.stdlib.BoundLogger, logger)
