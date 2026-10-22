from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from workflaskcode.config.paths import AgentHome
from workflaskcode.utils.fs import restrict_permissions
from workflaskcode.utils.redact import install_log_redaction

LOG_NAME = "workflaskcode"
MAX_LOG_BYTES = 1_000_000
LOG_BACKUPS = 3


def setup_logging(home: AgentHome, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        return logger
    home.logs_dir.mkdir(parents=True, exist_ok=True)
    path = home.logs_dir / "workflaskcode.log"
    handler = RotatingFileHandler(path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    logger.addHandler(handler)
    install_log_redaction(logger)
    restrict_permissions(path, 0o600)
    return logger
