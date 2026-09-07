"""Logging configuration.

Two things are arranged here.

**The application's own log lines actually appear.** `log_level` existed as a
setting for several phases without anything reading it, so `logger.info(...)`
calls throughout the codebase went nowhere. The one that matters most is the
coach falling back to the deterministic scheduler: the product is designed to
carry on regardless (invariant 4), which is exactly why a silent fallback is
easy to miss.

**Uvicorn's loggers are left alone.** It configures its own, and its access log
is already the per-request line most debugging starts from. Reconfiguring it
here would mean owning that formatting forever, for no gain.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from dsa_coach.config import Settings

FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"

#: Rotation, so a long-running server cannot fill the disk. Bodies make the file
#: grow far faster than the access log alone, which is what these are sized for.
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 3


def configure_logging(settings: Settings) -> None:
    """Send `dsa_coach` logs to the console, and to a file when one is set.

    Idempotent: handlers are replaced rather than appended, so `--reload` does
    not multiply every line by the number of restarts.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logger = logging.getLogger("dsa_coach")
    logger.setLevel(level)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()

    formatter = logging.Formatter(FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    if settings.log_file:
        path = Path(settings.log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # Ours are the only handlers; without this the root logger prints each line
    # a second time.
    logger.propagate = False
